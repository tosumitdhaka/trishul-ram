#!/usr/bin/env python3
"""Disable all leftover pipelines in the currently-deployed TRAM (via API).

The manager/standalone PVC carries 37 demo/study pipelines from previous
sessions; several are interval-scheduled (30-300s) and fire mid-measurement,
stealing worker CPU. The controlled A/B needs a clean scheduler.
"""
import json
import sys
import urllib.request

import yaml

API = "http://127.0.0.1:30001"
KEEP = set()  # nothing is kept enabled


def api(path, method="GET", data=None, ct="application/json"):
    req = urllib.request.Request(API + path, method=method)
    if data is not None:
        req.add_header("Content-Type", ct)
        req.data = data.encode() if isinstance(data, str) else json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode()
    return body


def main():
    pipelines = json.loads(api("/api/pipelines?limit=200"))
    print(f"found {len(pipelines)} pipelines")
    changed = 0
    for p in pipelines:
        name = p["name"]
        if name in KEEP:
            continue
        detail = json.loads(api(f"/api/pipelines/{name}"))
        doc = yaml.safe_load(detail["yaml"])
        body = doc.get("pipeline", doc) if isinstance(doc, dict) else None
        if body is None or not isinstance(body, dict):
            print(f"skip (unparsable): {name}")
            continue
        if body.get("enabled") is False:
            continue
        body["enabled"] = False
        new_yaml = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
        api(f"/api/pipelines/{name}", method="PUT", data=new_yaml, ct="text/yaml")
        changed += 1
        print(f"disabled: {name}")
    print(f"disabled {changed} pipelines")


if __name__ == "__main__":
    sys.exit(main())
