"""Isolate the hang axis: BaseHTTPMiddleware x {uvloop, httptools} in a thread."""
import sys
import threading
import time
import urllib.request

import uvicorn
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware


class PassthroughMW(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


CASE = sys.argv[1]
app = FastAPI()
app.add_middleware(PassthroughMW)


@app.post("/ingest")
async def ingest():
    return {"ok": True}


KWARGS = {
    "uv_ht": dict(loop="uvloop", http="httptools"),
    "uv_h11": dict(loop="uvloop", http="h11"),
    "as_ht": dict(loop="asyncio", http="httptools"),
    "as_h11": dict(loop="asyncio", http="h11"),
}[CASE]
PORT = 8802

t = threading.Thread(target=uvicorn.run, args=(app,),
                     kwargs=dict(host="127.0.0.1", port=PORT, log_config=None, **KWARGS),
                     daemon=True)
t.start()
time.sleep(3)

req = urllib.request.Request(f"http://127.0.0.1:{PORT}/ingest", data=b'{"x": 1}',
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(f"[mw-{CASE}] POST -> {r.status}")
except Exception as exc:  # noqa: BLE001
    print(f"[mw-{CASE}] POST -> {type(exc).__name__}: {str(exc)[:50]}")
sys.exit(0)
