"""Tests for the connectors router — test and test-pipeline endpoints."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.connectors import _extract_host, _extract_port, router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


_MINIMAL_YAML = """\
name: test-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


class TestTestConnector:
    def test_always_returns_200(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/connectors/test", json={"type": "unknown_type", "config": {}})
        assert resp.status_code == 200

    def test_unknown_type_no_host_returns_no_test_available(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/connectors/test", json={
            "type": "unknown_type",
            "config": {},
        })
        data = resp.json()
        assert data["ok"] is True
        assert "No test available" in data["detail"]

    def test_tcp_probe_failure_returns_ok_false(self):
        app = _make_app()
        client = TestClient(app)
        # Point at a port that should be closed
        resp = client.post("/api/connectors/test", json={
            "type": "clickhouse",
            "config": {"host": "127.0.0.1", "port": 19999},  # unlikely to be open
        })
        data = resp.json()
        assert data["ok"] is False


class TestTestPipeline:
    def test_no_yaml_returns_error(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/connectors/test-pipeline", json={"yaml_text": ""})
        assert resp.status_code == 200
        assert "error" in resp.json()

    def test_invalid_yaml_returns_parse_error(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/connectors/test-pipeline", json={"yaml_text": "invalid: ["})
        data = resp.json()
        assert "error" in data
        assert "YAML" in data["error"]

    def test_valid_yaml_returns_source_and_sinks(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/connectors/test-pipeline", json={"yaml_text": _MINIMAL_YAML})
        assert resp.status_code == 200
        data = resp.json()
        assert "source" in data
        assert "sinks" in data
        assert data["source"]["type"] == "local"

    def test_yaml_content_type_accepted(self):
        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/connectors/test-pipeline",
            content=_MINIMAL_YAML.encode(),
            headers={"Content-Type": "text/yaml"},
        )
        assert resp.status_code == 200
        assert "source" in resp.json()


class TestExtractHelpers:
    def test_extract_host_from_host_field(self):
        assert _extract_host("clickhouse", {"host": "myhost"}) == "myhost"

    def test_extract_host_from_brokers(self):
        assert _extract_host("kafka", {"brokers": ["kafka1:9092"]}) == "kafka1"

    def test_extract_host_from_url(self):
        host = _extract_host("amqp", {"url": "amqp://rabbit.local:5672/vhost"})
        assert host == "rabbit.local"

    def test_extract_host_empty_when_no_info(self):
        assert _extract_host("unknown", {}) == ""

    def test_extract_port_from_port_field(self):
        assert _extract_port("clickhouse", {"port": 9440}) == 9440

    def test_extract_port_from_brokers(self):
        assert _extract_port("kafka", {"brokers": ["kafka1:9092"]}) == 9092

    def test_extract_port_from_defaults(self):
        assert _extract_port("clickhouse", {}) == 9000
        assert _extract_port("kafka", {}) == 9092
        assert _extract_port("mqtt", {}) == 1883

    def test_extract_port_unknown_type(self):
        assert _extract_port("unknown_type", {}) == 0
