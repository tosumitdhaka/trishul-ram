"""Tests for v0.7.0 AppConfig additions: node_id, db_url, shutdown_timeout."""
from __future__ import annotations

import socket


def test_node_id_defaults_to_hostname(monkeypatch):
    monkeypatch.delenv("TRAM_NODE_ID", raising=False)
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.node_id == socket.gethostname()


def test_node_id_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_NODE_ID", "worker-3")
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.node_id == "worker-3"


def test_db_url_empty_by_default(monkeypatch):
    monkeypatch.delenv("TRAM_DB_URL", raising=False)
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.db_url == ""


def test_db_url_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_DB_URL", "sqlite:////tmp/x.db")
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.db_url == "sqlite:////tmp/x.db"


def test_shutdown_timeout_default(monkeypatch):
    monkeypatch.delenv("TRAM_SHUTDOWN_TIMEOUT_SECONDS", raising=False)
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.shutdown_timeout == 30


def test_shutdown_timeout_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_SHUTDOWN_TIMEOUT_SECONDS", "60")
    from importlib import reload

    import tram.core.config as cfg_mod
    reload(cfg_mod)
    config = cfg_mod.AppConfig.from_env()
    assert config.shutdown_timeout == 60
