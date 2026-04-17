"""Tests for v0.7.0 AppConfig additions: node_id, db_url, shutdown_timeout."""
from __future__ import annotations

import socket

import tram.core.config as cfg_mod


def test_node_id_defaults_to_hostname(monkeypatch):
    monkeypatch.delenv("TRAM_NODE_ID", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.node_id == socket.gethostname()


def test_node_id_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_NODE_ID", "worker-3")
    config = cfg_mod.AppConfig.from_env()
    assert config.node_id == "worker-3"


def test_db_url_empty_by_default(monkeypatch):
    monkeypatch.delenv("TRAM_DB_URL", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.db_url == ""


def test_db_url_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_DB_URL", "sqlite:////tmp/x.db")
    config = cfg_mod.AppConfig.from_env()
    assert config.db_url == "sqlite:////tmp/x.db"


def test_shutdown_timeout_default(monkeypatch):
    monkeypatch.delenv("TRAM_SHUTDOWN_TIMEOUT_SECONDS", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.shutdown_timeout == 30


def test_shutdown_timeout_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_SHUTDOWN_TIMEOUT_SECONDS", "60")
    config = cfg_mod.AppConfig.from_env()
    assert config.shutdown_timeout == 60


def test_stats_interval_default(monkeypatch):
    monkeypatch.delenv("TRAM_STATS_INTERVAL", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.stats_interval == 30


def test_stats_interval_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_STATS_INTERVAL", "15")
    config = cfg_mod.AppConfig.from_env()
    assert config.stats_interval == 15


def test_worker_ingress_port_default(monkeypatch):
    monkeypatch.delenv("TRAM_WORKER_INGRESS_PORT", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.worker_ingress_port == 8767


def test_worker_ingress_port_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_WORKER_INGRESS_PORT", "9001")
    config = cfg_mod.AppConfig.from_env()
    assert config.worker_ingress_port == 9001
