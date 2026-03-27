"""Connector connectivity test endpoint."""

from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from fastapi import APIRouter, Request

router = APIRouter()

_TIMEOUT_S = 10


@router.post("/api/connectors/test")
async def test_connector(request: Request) -> dict:
    """Test connectivity for a single connector type + config.

    Always returns HTTP 200. ``ok`` field indicates pass/fail.
    """
    body = await request.json()
    conn_type = body.get("type", "")
    config = body.get("config", {})
    return _do_test(conn_type, config)


@router.post("/api/connectors/test-pipeline")
async def test_pipeline_connectors(request: Request) -> dict:
    """Parse pipeline YAML and test all connectors (source + sinks) in parallel.

    Accepts raw YAML text or JSON {yaml_text: ...}.
    """
    content_type = request.headers.get("content-type", "")
    if "yaml" in content_type or "text" in content_type or "plain" in content_type:
        yaml_text = (await request.body()).decode()
    else:
        body = await request.json()
        yaml_text = body.get("yaml_text", "")

    if not yaml_text:
        return {"source": None, "sinks": [], "error": "no YAML provided"}

    try:
        from tram.pipeline.loader import load_pipeline_from_yaml
        config = load_pipeline_from_yaml(yaml_text)
    except Exception as exc:
        return {"source": None, "sinks": [], "error": f"YAML parse error: {exc}"}

    source_type = config.source.type
    source_cfg = config.source.model_dump(exclude_none=True)

    sink_entries = []
    for s in (config.sinks or []):
        sink_cfg = s.model_dump(exclude_none=True)
        sink_entries.append((s.type, sink_cfg))

    with ThreadPoolExecutor(max_workers=max(1, 1 + len(sink_entries))) as ex:
        src_future = ex.submit(_do_test, source_type, source_cfg)
        sink_futures = [ex.submit(_do_test, t, c) for t, c in sink_entries]

        source_result = {"type": source_type, **_safe_get(src_future)}
        sink_results = [
            {"type": sink_entries[i][0], **_safe_get(f)}
            for i, f in enumerate(sink_futures)
        ]

    return {"source": source_result, "sinks": sink_results}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _safe_get(future) -> dict:
    try:
        return future.result(timeout=_TIMEOUT_S + 1)
    except FuturesTimeout:
        return {"ok": False, "latency_ms": None, "error": f"Timeout after {_TIMEOUT_S}s"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "error": str(exc)}


def _do_test(conn_type: str, config: dict) -> dict:
    """Run the appropriate test for a connector type, with timeout."""
    # Ensure plugins are registered
    try:
        import tram.connectors  # noqa: F401
    except Exception:
        pass

    from tram.registry.registry import _sources, _sinks

    plugin_cls = _sources.get(conn_type) or _sinks.get(conn_type)

    if plugin_cls and hasattr(plugin_cls, "test_connection"):
        instance = plugin_cls.__new__(plugin_cls)
        instance.config = config
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(instance.test_connection)
            try:
                return future.result(timeout=_TIMEOUT_S)
            except FuturesTimeout:
                return {"ok": False, "latency_ms": None, "error": f"Timeout after {_TIMEOUT_S}s"}
            except NotImplementedError:
                pass  # fall through to TCP probe
            except Exception as exc:
                return {"ok": False, "latency_ms": None, "error": str(exc)}

    # Generic TCP probe
    host = _extract_host(conn_type, config)
    port = _extract_port(conn_type, config)
    if host and port:
        return _tcp_probe(host, port)

    return {"ok": True, "latency_ms": None, "detail": f"No test available for '{conn_type}'"}


def _extract_host(conn_type: str, config: dict) -> str:
    if config.get("host"):
        return config["host"]
    brokers = config.get("brokers") or []
    if brokers:
        return (brokers[0] if isinstance(brokers, list) else brokers).split(":")[0]
    hosts = config.get("hosts") or []
    if hosts:
        h = (hosts[0] if isinstance(hosts, list) else hosts)
        return h.replace("https://", "").replace("http://", "").split(":")[0].split("/")[0]
    servers = config.get("servers") or []
    if servers:
        s = (servers[0] if isinstance(servers, list) else servers)
        return s.replace("nats://", "").replace("tcp://", "").split(":")[0]
    return ""


def _extract_port(conn_type: str, config: dict) -> int:
    if config.get("port"):
        try:
            return int(config["port"])
        except (ValueError, TypeError):
            pass
    brokers = config.get("brokers") or []
    if brokers:
        b = (brokers[0] if isinstance(brokers, list) else brokers)
        parts = b.split(":")
        if len(parts) > 1:
            try:
                return int(parts[-1])
            except ValueError:
                pass
    servers = config.get("servers") or []
    if servers:
        s = (servers[0] if isinstance(servers, list) else servers)
        parts = s.replace("nats://", "").replace("tcp://", "").split(":")
        if len(parts) > 1:
            try:
                return int(parts[-1])
            except ValueError:
                pass
    defaults = {
        "kafka": 9092, "mqtt": 1883, "nats": 4222, "amqp": 5672,
        "redis": 6379, "influxdb": 8086, "opensearch": 9200,
        "elasticsearch": 9200, "clickhouse": 9000,
    }
    return defaults.get(conn_type, 0)


def _tcp_probe(host: str, port: int) -> dict:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT_S):
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "latency_ms": latency, "detail": f"TCP {host}:{port} OK"}
    except Exception as exc:
        return {"ok": False, "latency_ms": None, "error": f"TCP {host}:{port} failed: {exc}"}
