#!/usr/bin/env python3
"""Dual-purpose mock REST endpoint for the REST-source / REST-sink bench scenarios.

* ``GET  /records?page=N``  serves the corpus in pages (``--page-size``) as a
  JSON array. The TRAM REST source's paginator is also supported natively:
  ``GET /records?offset=N&limit=M`` (the source drives ``offset`` and
  ``limit`` query params).
* ``POST /collect``         counts received records and body bytes — the
  counting target for the REST sink.
* ``GET  /collect/stats``   returns the counters accumulated so far
  (``?reset=1`` zeroes them).

Runs in the foreground on ``--port`` (default 18080). Backed by FastAPI +
uvicorn (both in the harness venv).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="tram-perf-mock-rest")

CORPUS: list[dict] = []
PAGE_SIZE = 100
STATS = {"records": 0, "bytes": 0, "posts": 0}


@app.get("/")
async def info() -> dict:
    return {
        "service": "tram-perf-mock-rest",
        "corpus_records": len(CORPUS),
        "page_size": PAGE_SIZE,
        "collected": STATS,
    }


@app.get("/records")
async def records(request: Request) -> JSONResponse:
    params = request.query_params
    offset = 0
    limit = PAGE_SIZE
    if "offset" in params:
        offset = int(params["offset"])
        limit = int(params.get("limit", PAGE_SIZE))
    elif "page" in params:
        page = int(params["page"])
        offset = page * PAGE_SIZE
        limit = PAGE_SIZE
    if "limit" in params and "page" in params:
        limit = int(params["limit"])
    page_items = CORPUS[offset : offset + limit]
    return JSONResponse(
        page_items,
        headers={
            "X-Total-Count": str(len(CORPUS)),
            "X-Page-Size": str(PAGE_SIZE),
        },
    )


@app.post("/collect")
async def collect(request: Request) -> dict:
    raw = await request.body()
    n_records = 0
    if raw:
        try:
            data = json.loads(raw)
            n_records = len(data) if isinstance(data, list) else 1
        except json.JSONDecodeError:
            n_records = 0
    STATS["records"] += n_records
    STATS["bytes"] += len(raw)
    STATS["posts"] += 1
    return {"ok": True, "records": n_records, "bytes": len(raw)}


@app.get("/collect/stats")
async def collect_stats(request: Request) -> dict:
    if request.query_params.get("reset") == "1":
        STATS["records"] = 0
        STATS["bytes"] = 0
        STATS["posts"] = 0
    return dict(STATS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mock REST endpoint serving the CDR corpus in pages + counting POST sink."
    )
    parser.add_argument("--port", type=int, default=18080, help="Listen port (default 18080)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--corpus", required=True, help="JSONL corpus file (gen_corpus.py output)")
    parser.add_argument("--page-size", type=int, default=100, help="Records per page (default 100)")
    args = parser.parse_args()

    global PAGE_SIZE
    PAGE_SIZE = args.page_size

    lines = Path(args.corpus).read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.strip():
            CORPUS.append(json.loads(line))
    if not CORPUS:
        raise SystemExit(f"mock_rest_server: empty corpus file {args.corpus}")
    print(
        f"mock_rest_server: {len(CORPUS)} records loaded; listening on {args.host}:{args.port}",
        file=sys.stderr,
    )

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()