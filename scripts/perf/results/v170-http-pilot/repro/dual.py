"""Bisect: TWO accel uvicorn servers in one process (worker topology) — agent + ingress."""
import sys
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI

CASE = sys.argv[1]
KWARGS = {
    "uv_ht": dict(loop="uvloop", http="httptools"),
    "as_h11": dict(loop="asyncio", http="h11"),
}[CASE]

app = FastAPI()


@app.post("/ingest")
async def ingest():
    return {"ok": True}


def run(port):
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, **KWARGS)


t1 = threading.Thread(target=run, args=(8811,), daemon=True)
t1.start()
time.sleep(2)
t2 = threading.Thread(target=run, args=(8812,), daemon=True)
t2.start()
time.sleep(3)

for port in (8811, 8812):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/ingest", data=b'{"x": 1}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"[dual-{CASE}] POST :{port} -> {r.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"[dual-{CASE}] POST :{port} -> {type(exc).__name__}: {str(exc)[:50]}")
sys.exit(0)
