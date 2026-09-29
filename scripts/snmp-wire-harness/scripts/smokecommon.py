"""Shared helpers for the tsmi/tsmp v0.5.x re-validation smoke harness."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

RESULTS = Path("/tmp/opencode/tsmi-smoke-v06/results")


def record(check: str, ok: bool, evidence: str) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    line = f"[{'PASS' if ok else 'FAIL'}] {check}: {evidence}"
    print(line, flush=True)
    slug = check.replace("/", "-").replace(" ", "-").replace(":", "-")
    with (RESULTS / f"{slug}.json").open("w") as fh:
        json.dump({"check": check, "ok": ok, "evidence": evidence}, fh, indent=2)


def main(coro_fn) -> None:
    import asyncio

    try:
        asyncio.run(coro_fn())
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
