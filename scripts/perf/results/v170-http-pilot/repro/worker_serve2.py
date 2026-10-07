"""Repro 3: accelerated worker with a REGISTERED webhook queue (simulated source).

If POST 202s fast under uv_ht with the queue present, the kind-cluster "hang"
was the placement window on an unadopted pipeline, not the runtime.
Usage: worker_serve2.py <0|1>
"""
import queue
import sys
import threading
import time
import urllib.request

FLAG = sys.argv[1]
import os

os.environ["TRAM_HTTP_ACCELERATED"] = FLAG

from tram.core.config import AppConfig  # noqa: E402
from tram.daemon.server import serve  # noqa: E402

config = AppConfig(
    host="127.0.0.1", port=8765, pipeline_dir="./pipelines", state_dir=None,
    api_url="http://localhost:8765", log_level="WARNING", log_format="json",
    workers=1, reload_on_start=False, node_id="repro-w1", db_url="",
    shutdown_timeout=10, api_key="", rate_limit=0, rate_limit_window=60,
    tls_certfile="", tls_keyfile="", otel_endpoint="", otel_service="tram",
    watch_pipelines=False, mib_dir="/mibs", schema_dir="/schemas",
    schema_registry_url="", schema_registry_username="", schema_registry_password="",
    ui_dir="/ui", auth_users="", templates_dir="/tram-templates",
    tram_mode="worker", manager_url="", stats_interval=30, worker_urls="",
    worker_replicas=0, worker_service="tram-worker", worker_namespace="default",
    worker_port=8866, worker_ingress_port=8867,
)

threading.Thread(target=serve, args=(config,), daemon=True).start()
time.sleep(5)

from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY  # noqa: E402

q = queue.Queue()
with _REGISTRY_LOCK:
    _WEBHOOK_REGISTRY["ingest"] = q
print("registered queue for path 'ingest'")


def probe():
    req = urllib.request.Request("http://127.0.0.1:8867/webhooks/ingest",
                                 data=b'{"x": 1}',
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return f"{r.status} in {(time.monotonic() - t0) * 1000:.0f}ms"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:50]} in {(time.monotonic() - t0) * 1000:.0f}ms"


for i in range(3):
    print(f"[flag={FLAG}] POST #{i + 1} -> {probe()}")
sys.exit(0)
