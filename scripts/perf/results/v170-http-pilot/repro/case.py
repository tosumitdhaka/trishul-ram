"""Minimal repro for the Pilot A POST hang: threaded uvicorn + explicit accel kwargs.

Usage: case.py <case>
  main_uv_ht   — main-thread server, loop=uvloop http=httptools   (control: manager)
  th_uv_ht      — thread server,   loop=uvloop http=httptools      (worker shape)
  th_uv         — thread server,   loop=uvloop http=h11
  th_ht         — thread server,   loop=asyncio http=httptools
  th_none       — thread server,   no loop/http kwargs (auto)
"""
import sys
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI

CASE = sys.argv[1]
app = FastAPI()


@app.post("/ingest")
async def ingest():
    return {"ok": True}


@app.get("/ping")
async def ping():
    return {"pong": True}


KWARGS = {
    "main_uv_ht": dict(loop="uvloop", http="httptools"),
    "th_uv_ht": dict(loop="uvloop", http="httptools"),
    "th_uv": dict(loop="uvloop", http="h11"),
    "th_ht": dict(loop="asyncio", http="httptools"),
    "th_none": dict(),
}[CASE]
PORT = 8801


def probe(method, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/{'ingest' if body is not None else 'ping'}",
        data=body, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return f"{r.status}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:60]}"


if CASE.startswith("th_"):
    t = threading.Thread(target=uvicorn.run, args=(app,),
                         kwargs=dict(host="127.0.0.1", port=PORT, log_config=None, **KWARGS),
                         daemon=True)
    t.start()
else:
    threading.Thread(target=lambda: time.sleep(30), daemon=True).start()
    import asyncio

    async def _run():
        config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_config=None, **KWARGS)
        server = uvicorn.Server(config)
        await server.serve()

    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()

time.sleep(3)
print(f"[{CASE}] GET  -> {probe('GET')}")
print(f"[{CASE}] POST -> {probe('POST', b'{\"x\": 1}')}")
time.sleep(1)
sys.exit(0)
