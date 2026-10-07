#!/usr/bin/env python3
"""Generate merged helm values for the v1.6.0 re-run: topo x profile.

mw-{L,M,H}:    values-bench + values-res-{p}   (bench already carries mgr+worker topo)
single-{L,M,H}: values-bench + values-single + values-res-{p}

Both get: image tags pinned to the new build, TRAM_MANAGER_URL pinned per
topology (#86 lesson: prevent the callback-URL env leak across topology
switches), and PERF_SFTP_REMOTE=/upload/in (atmoz chroot fix).
"""
import sys
import yaml

REPO = "/home/dhaka/trishul/trishul-ram"
INFRA = f"{REPO}/scripts/perf/infra"
OUT = "/tmp/opencode/v160-rerun/values"
TAG = sys.argv[1]


def load(p):
    with open(p) as f:
        return yaml.safe_load(f) or {}


def deep_merge(base, overlay):
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


for prof in ("L", "M", "H"):
    # mgr+worker
    doc = load(f"{INFRA}/values-bench.yaml")
    deep_merge(doc, load(f"{INFRA}/values-res-{prof}.yaml"))
    doc["manager"]["image"]["tag"] = TAG
    doc["worker"]["image"]["tag"] = TAG
    doc["env"]["TRAM_MANAGER_URL"] = "http://trishul-ram:8765"
    doc["env"]["PERF_SFTP_REMOTE"] = "/upload/in"
    with open(f"{OUT}/mw-{prof}.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    # single
    doc = load(f"{INFRA}/values-bench.yaml")
    deep_merge(doc, load(f"{INFRA}/values-single.yaml"))
    deep_merge(doc, load(f"{INFRA}/values-res-{prof}.yaml"))
    doc["image"]["tag"] = TAG
    doc["env"]["TRAM_MANAGER_URL"] = "http://localhost:8765"
    doc["env"]["PERF_SFTP_REMOTE"] = "/upload/in"
    with open(f"{OUT}/single-{prof}.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    print(f"wrote mw-{prof}.yaml single-{prof}.yaml (tag={TAG})")
